# Native compact peer-message patch

Upstream: public openai/codex rust-v0.170.0. Annotated tag object
`72f97c309fb0f78b6f35cce455adf416e6a51571` peels to commit
`29eb79e9866b3262a777fdf8195c1b9e63684c91`.

Apply `compact-peer-messages.patch` with `git apply --check`, then `git apply`
in a clean checkout of that commit. Then apply `workspace-release-lock.patch`
(the reviewed release metadata correction below). Run from codex-rs:

```
cargo test --locked --release -p codex-tui --lib compact_peer -- --nocapture
cargo build --locked --release -p codex-cli --bin codex
```

The manually dispatched or narrowly branch/path-triggered build-codex-compact workflow verifies both upstream IDs,
uses upstream Rust 1.95.0, runs the targeted tests (including an inline snapshot),
and uploads the Windows x64 release CLI. It neither publishes nor installs it.
After review, a push to feature/codex-compact-renderer touching only the specified patch/workflow paths triggers the initial build; workflow_dispatch remains available once registered. Building requires
network access for upstream dependencies and substantial runner disk/time.

Renderer integration: UserHistoryCell compact_hyperlink_lines decodes only the
exact nested ai-sessions wrapper and clips the actual body to one display row.
activity_ids returns a stable namespaced message UUID; the existing retained
TranscriptView content_presentation then enables native disclosure controls.
expanded_hyperlink_lines defaults to the patched transcript_hyperlink_lines,
which renders the decoded body and sender. The pinned prompt header also uses the decoded view. No transport/input/state is rewritten.
The retained layout regression exercises collapsed, expanded, then collapsed
presentation through the actual cell_content_layout and entry_layout/cache routes; this is not an overlay
substitute. Collapsed peer previews reuse row zero as the native disclosure control, without an added control or separator row. Expanded messages retain the native Hide details control.

Unknown/malformed wrappers, mismatched outer/inner identity, invalid UUIDs and
unsafe sender labels fall through unchanged. Missing sender_handle uses tool:id8.
Labels are informational and unverified; wrapper security instructions remain in
original agent input and raw output. Rendered copy/full transcript uses the decoded
body; raw mode intentionally retains the complete original wrapper. The display
preview collapses whitespace and uses native Unicode-aware clipping.

Verification: fast Windows candidate run 44706087638 built Codex 0.170.0 and
passed the targeted compact_peer Rust tests. The parent native-console fixture
passed all four actual-console assertions: initially collapsed, F4/Enter expansion,
tail visible after scrolling, and Enter collapse again. The parent installed the
verified custom interactive executable; stock RPC remains separate. Collapsed peer cells occupy exactly one row, including consecutive peer cells and narrower retained reflow. Click the preview row or use native activity keyboard expansion; expanded content has the ordinary native collapse control. Raw mode does not collapse. This patch targets only the exact
pinned source/envelope; future upstream or envelope changes require review.

Local checks: git diff --check passed in upstream scratch checkout; reverse application, clean forward git apply --check, and forward application all passed. No Rust tests or release build were run locally.


## Windows lock diagnostics

If the locked test step fails, the workflow performs exactly one diagnostic
`cargo metadata --offline --format-version 1` resolution against the pinned
checkout, then restores the original Cargo.lock byte for byte. It uploads
`codex-0.170.0-windows-lock-diagnostics` with the original/proposed lockfiles,
a unified Cargo.lock.diff, package inventory changes, metadata output/errors,
and hash/restoration summary. The probe has a ten-minute timeout and performs
no build. An offline cache miss may prevent full resolution; its error and any
partial proposed lock change remain available for inspection.

The workflow remains failed and never builds against an unreviewed dependency
graph. Both the regression test and release build commands retain --locked.
Review that artifact before adding a minimal pinned lockfile patch; no inferred
lockfile change is bundled without the Windows resolution evidence.

Fixture-only helper checks (no Cargo required):
`python patches/codex-compact-messages/test_lock_diagnostics.py`


## Reviewed release-tag lock correction

Run 44691612091's `codex-0.170.0-windows-lock-diagnostics` artifact established
that the tag manifests set workspace version 0.170.0 while Cargo.lock retains
0.0.0 for all 174 local packages. Offline Cargo metadata proposed precisely
174 substitutions from 0.0.0 to 0.170.0. Independent comparison against the
pinned manifests confirmed all 174 local package versions. The other 1,310
external package records, their checksums/source pins, every dependency list,
and top-level lock metadata are unchanged. The diagnostic exited 101 later
when its offline cache lacked the Inflector crate archive; the proposed lock
had already been written and the original was restored.

`workspace-release-lock.patch` is that exact UTF-8/LF artifact diff, separately
reviewable from the renderer patch. The workflow requires these SHA256 hashes:

- Original: `e913b4fe3ff12121362c1f85ef360b32cba72305149c0cbe55e0a416337c1415`
- Corrected: `11ed3c419dcb08d7d3921f20eb4980c7a0620234c1f7ed79a2d3ca3b98f39a3e`

These are local workspace release labels, not external dependency upgrades.
The upstream commit/tag pin, Rust toolchain, and --locked test/build commands
remain unchanged. The fast Windows build and targeted tests passed in run
44706087638. Applying this correction alone does not establish runtime correctness.

## Redistribution

`redistribution/LICENSE` and `redistribution/NOTICE` are byte-for-byte copies
from the pinned upstream commit. `MODIFICATIONS.txt` identifies this custom build.
After verification, package an explicitly supplied executable with:

```
python patches/codex-compact-messages/package_binary.py --exe PATH/codex.exe --output PATH/codex-compact.zip --run BUILD_RUN_URL
```

The ZIP includes the executable, notices, both source patches, and BUILD.json
with upstream identity and executable SHA256. The helper validates the ZIP and
embedded binary and writes a neighboring `.zip.sha256` file. It never installs
or publishes a binary. The fast candidate uses an unoptimized dev profile with
debug information and LTO disabled; optimized release assets are separate.
