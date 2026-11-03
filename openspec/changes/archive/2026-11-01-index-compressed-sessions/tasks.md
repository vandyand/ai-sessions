## 1. Compression core

- [x] 1.1 Add `compression.py` with verified compress and decompress; verify with `tests/test_compression.py`.
- [x] 1.2 Add `open_for_scan` and the `zstandard` dependency; verify with `tests/test_compression.py`.

## 2. Model and gates

- [x] 2.1 Add `Availability.COMPRESSED`, `NativeSession.compressed`, `Session.compressed`, with catalog migration; verify with `tests/test_compressed_sessions.py`.
- [x] 2.2 Report `compressed` from `native_session_availability` and refuse it in `read_snapshot` and `read_since`, normalising to the plain path; verify with `tests/test_compressed_sessions.py`.

## 3. Discovery

- [x] 3.1 Claude Code discovery lists `.jsonl.zst` sessions and subagents, plain winning; resolve, exists and availability handle both forms; verify with `tests/test_compressed_discovery.py`.
- [x] 3.2 Codex discovery lists rollouts whose database path exists only as `.zst`; resolve, exists and availability handle both forms; verify with `tests/test_compressed_discovery.py`.
- [x] 3.3 Discovery caches: one-shot entries, no decompression on repeat, carry-over from the plain entry; verify with `tests/test_compressed_discovery.py`.

## 4. Filter and display

- [x] 4.1 `compression` filter in `filtered_sessions`, `--compression`, key `z`, header, help and README; verify with `tests/test_compressed_browser.py`.
- [x] 4.2 STATUS reads `compressed`, `--list` shows it, details pane states it; verify with `tests/test_compressed_browser.py`.
- [x] 4.3 Header reports matching sessions excluded only by the filter; verify with `tests/test_compressed_browser.py`.

## 5. Milestone check

- [x] 5.1 Full suite, Ruff and `openspec validate --strict`; real session list checked read-only with the installed wheel, including StarCraft-style rollouts and cold index time.
