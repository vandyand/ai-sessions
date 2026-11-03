# Retention build: acceptance report

Date: 2026-11-01. Branch `supervisor-sessions`, not pushed. Commits `85d7821`, `8709780`, `61c7fce`
and the archive commit that follows them. Installed: wheel 3.7.5 in Python 3.12.

## Criteria

Each was checked against the final code.

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | Nothing deleted, Claude | Met | `~/.claude/settings.json` reads `cleanupPeriodDays` 36500 |
| 2 | Nothing deleted, Codex | Met | `BUILD.md` "What investigation established": no age deletion; `codex delete` is explicit only |
| 3 | Idle compression | Met, with the limit below | `tests/test_idle_compression.py`; live sweep in throwaway homes with the real hidden process, all checks pass (`live_m3.py`): idle Claude, Codex, hidden and subagent files compressed, recent left alone, a decompressed session gets a fresh 30 days. Open sessions refused from a real terminal |
| 4 | Safe compression | Met | `tests/test_compression.py`; real Node writer holding the file: compression refused and every byte kept (`live_writer.py`); interrupted runs tested by injected crashes |
| 5 | Compressed sessions listed | Met | 6,162 sessions listed; read-only audit of all 2,387 real `.zst` files: all restore in full, 8.77 GB, 0 damaged |
| 6 | Filter | Met | `z`, `--compression`, default uncompressed; `tests/test_compressed_browser.py`; live run shows hidden plus compressed filtering together |
| 7 | Actions | Met | `Z`, `--compress`, `--decompress`; real-harness run below |
| 8 | Transpile gate | Met | Real run: bridge and `--resume` refuse a compressed session and give the command |
| 9 | Opening | Met | Browser asks, then decompresses; tests in `tests/test_compressed_browser.py` |
| 10 | Links survive | Met | `tests/test_compressed_links.py` passes; real run compressed and restored a linked member with no error |
| 11 | Speed | Met | Browser catalog 0.9 s; full listing about 9 s, in line with the earlier 8 s |
| 12 | Live proof | Met | Real Claude and Codex in throwaway homes, rerun on the final core: 22 of 24 checks pass. Claude resumed with its memory after a byte-identical restore with the modification time kept; Codex resumed our `.zst` itself. The two failures are errors in the check script, which reads `launch_targets` where the copy is not recorded; the same two appeared at milestone 2 |
| 13 | Suite and Ruff | Met | 924 tests, 1 skipped, Ruff clean, `openspec validate --strict` passes; README, DESIGN and CHANGELOG updated |

## The real sweep

Dry run and real run both compressed 0 of 6,162 sessions, which is correct:

- Claude had already deleted anything older than 30 days, and every Codex session older than
  October 20 was already `.zst`.
- 335 Claude rows are over 30 days old, but all sit in seven sessions active since October 14.
- One session qualifies at 29 days, so the first real compression should come within a day or two.
  About 800 qualify over the next 20 days.

So automatic compression has not yet acted on a real session. It has acted only on throwaway data.

## Reviews

Two independent reviews looked for data loss. The first reproduced 12 defects, the second 7 more in
the fixes. All were fixed with tests except the items listed under limitations. The most serious:

- a short plain file could overwrite a full `.zst` (now refused, both kept);
- terminal commands ignored open sessions (now use live discovery);
- a process appending could lose writes (the file is now held with no sharing);
- a truncated `.zst` restored silently and was deleted (now refused and kept);
- a compress racing a restore could destroy both copies (restores now hold the file exclusively).

A third review of the second round of fixes was not run. Those fixes have tests and live checks.

## Decisions to review

- **Subagent families.** A session and its subagents are idle together. An old subagent stays plain
  until its whole family has been quiet for 30 days. This is stricter than "not reopened for 30
  days" and exists so that a live session is never split.
- **Hidden history blocks opening.** If a plain file does not contain the `.zst` beside it, the
  session will not open until someone resolves it, and the message says both files are kept. No
  such case exists today.
- **Liveness failures stop the run**, and `Z` and `--compress` refuse in the same situation.

## Known limitations

- Claude Code has no compressed format, so `claude --resume` outside `sessions` fails on a
  compressed session with "No conversation found". Scripts that do this need `--decompress` first.
- If a harness has a transcript loaded but no file handle open, and escapes liveness detection, it
  could write a new plain file after compression. Nothing is lost: both are kept and the session
  refuses to open with an explanation. Whether Claude Code keeps a handle was not measured.
- The sweep runs only when `sessions` starts.

## Scope ledger

- Stale lock taken by two runs at the same moment: harmless, each file operation is safe alone.
- `.partial` files left by a killed run are not cleaned up.
- A scheduled task for days `sessions` is not started; the charter rules it out.
- `--install-hooks` does not set `cleanupPeriodDays` on another machine.
- OpenCode retention: non-goal.
- Listing shows a truncated `.zst` as a short transcript with no warning; restoring it is refused.
  The audit found none.
- `Z` on a subagent row ignores the family rule; the exclusive open still guards it.
- A read-only plain file repeats the compression work on each sweep and then reports the failure.
