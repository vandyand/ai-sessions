## 1. Operation

- [x] 1.1 `transcript_files` capability in Claude Code and Codex, `Unsupported` in OpenCode; verify with `tests/test_retention.py`.
- [x] 1.2 `retention.py` `compress_session`, `decompress_session`, refusing open sessions; verify with `tests/test_retention.py`.

## 2. State and gates

- [x] 2.1 `_conversation_status` `compressed` list, blocker, `resolve_launch`, `bridge_for`; verify with `tests/test_compressed_links.py`.
- [x] 2.2 `can_bridge`, `prepare_launch`, `command_for`; verify with `tests/test_compressed_links.py`.
- [x] 2.3 Rename keeps a compressed Claude Code transcript untouched; verify with `tests/test_compressed_links.py`.
- [x] 2.4 Supervisor create refusal, compressed-target notice with checkpoint kept, compressed supervisor restored on `S`; verify with `tests/test_compressed_links.py`.

## 3. Browser and command line

- [x] 3.1 `Z`, including linked conversations and open refusal, help, footer, README; verify with `tests/test_compressed_browser.py`.
- [x] 3.2 Enter asks, decompresses, opens; decline changes nothing; verify with `tests/test_compressed_browser.py`.
- [x] 3.3 `--compress`, `--decompress`, `--resume` refusal; verify with `tests/test_compressed_browser.py`.

## 4. Milestone check

- [x] 4.1 Full suite, Ruff, `openspec validate --strict`; live round trip on a throwaway Claude Code and Codex session in isolated homes, including bridging refused then allowed.
