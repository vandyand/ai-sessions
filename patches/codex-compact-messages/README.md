# Native compact peer-message patch

Upstream: public openai/codex rust-v0.170.0. Annotated tag object
`72f97c309fb0f78b6f35cce455adf416e6a51571` peels to commit
`29eb79e9866b3262a777fdf8195c1b9e63684c91`.

Apply `compact-peer-messages.patch` with `git apply --check`, then `git apply`
in a clean checkout of that commit. Run from codex-rs:

```
cargo test --locked --release -p codex-tui --lib compact_peer -- --nocapture
cargo build --locked --release -p codex-cli --bin codex
```

The manually dispatched or narrowly branch/path-triggered build-codex-compact workflow verifies both upstream IDs,
uses upstream Rust 1.95.0, runs the targeted tests (including an inline snapshot),
and uploads the Windows x64 release CLI. It neither publishes nor installs it.
After review, a push to feature/compact-peer-messages touching only the specified patch/workflow paths triggers the initial build; workflow_dispatch remains available once registered. Building requires
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

Limitations: local Rust compilation and native interaction were unavailable;
CI must establish compilation and test success before using the binary. No mouse
or keyboard end-to-end test has been run. Collapsed peer cells occupy exactly one row, including consecutive peer cells and narrower retained reflow. Click the preview row or use native activity keyboard expansion; expanded content has the ordinary native collapse control. Raw mode does not collapse. This patch targets only the exact
pinned source/envelope; future upstream or envelope changes require review.

Local checks: git diff --check passed in upstream scratch checkout; reverse application, clean forward git apply --check, and forward application all passed. No Rust tests or release build were run locally.
