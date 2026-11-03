## 1. Idle rule

- [x] 1.1 Open log, `last_activity`, `idle_sessions`, hidden included; verify with `tests/test_idle_compression.py`.
- [x] 1.2 `compress_after_days` in config, with invalid values falling back; verify with `tests/test_idle_compression.py`.
- [x] 1.3 Opening and decompressing record activity; verify with `tests/test_idle_compression.py`.
- [x] 1.4 A session and its subagents are idle together; verify with `tests/test_idle_compression.py` and the live sweep.

- [x] 1.5 Findings of the data-loss review fixed; verify with `tests/test_compression.py`, `tests/test_retention.py`, `tests/test_idle_compression.py` and a live Node writer.

## 2. Run

- [x] 2.1 `run_sweep` with per-session re-check, failure isolation and dry run; verify with `tests/test_idle_compression.py`.
- [x] 2.2 Lock and six-hour gate; verify with `tests/test_idle_compression.py`, including concurrent starts.
- [x] 2.3 `--compress-idle`, hidden background start before the browser, log; verify with `tests/test_idle_compression.py`.

## 3. Documentation

- [x] 3.1 README, DESIGN and CHANGELOG cover retention, keys, commands, the setting and Claude Code's limit; verify by reading.

## 4. Milestone check

- [x] 4.1 Full suite, Ruff, `openspec validate --strict`; live sweep on a throwaway home; real dry run reviewed; real sweep run and checked.
