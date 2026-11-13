# Vanilla session launch and alias restart

User authorized cdsp/cdba restart support and proposed a shared new-session CLI.

Design: `sessions new TOOL -- NATIVE_ARGS` starts the genuine native CLI with
inherited stdin/stdout/console and original arguments. No embedded terminal,
PTY replacement, custom input handling, permission policy changes, or prompt
replay. `cdsp`/`cdba` route through this shared command with their existing bypass
flags. The normal raw `claude`/`codex` commands remain available.

Launcher owns only its native child PID/generation, polls the provider's exact
native process-to-session registry/log metadata, and writes existing terminal
memory when identity changes. Existing sesres uses that exact client identity,
spares shared daemons, and the waiting launcher resumes the current native ID.
Option metadata from native --help separates initial prompts/resume selectors
from reusable flags. Restart retains options and omits original prompt/images/
one-off fork/worktree actions. Provider hooks hold native formats outside generic
launcher code. No history conversion or all-catalog scan.

Tasks:
- [x] Adapter worker: native executable/current session capabilities and tests.
- [x] Parent: new command, direct console lifecycle and safe resume option parser;
      native/fake lifecycle regression tests, profile alias update, docs.
- [ ] Parent: isolated fullsuite, review, CI/release/install, real disposable alias
      tests. Keep unrelated main-checkout edits intact.
- [x] Research: current Windows Terminal workspaces and companion alternatives;
      no new desktop app or terminal switch without further user decision.

Native verification found that shared Codex 0.167.1 frontend logs omit IDs. Fresh
Codex identity is therefore assigned before launch using existing provider empty
session creation; known resume selection is made before the client starts. The
native log callback remains a verified optional observation, not a guessed owner.
Both real native restart tests passed with exact same IDs and new client PIDs.
