# Changelog

## 3.11.1 — 2026-11-07

### Fixed

- Same-harness restart (`sesres`) and `--reopen` now resume the exact native branch
  this tab ran, including forked/transpiled conversations with divergent heads.
  They no longer follow conversation-head selection or saved cross-harness preferences.
  Normal resumes and explicit cross-harness requests retain the divergence safeguards.
- Terminal memory matches both the harness and native session ID, avoiding a wrong
  selection when two harnesses share an ID.
- Restart commands run by a shared Codex daemon now find their own verified
  terminal client by native thread ID, while sparing the daemon and unrelated tabs.

## 3.11.0 — 2026-11-07

### Added

- Native same-machine session messaging: `sessions send`, `sessions reply`, and
  `sessions messaging status/install/uninstall`. Codex receives messages through
  its owning daemon, steering active turns and starting idle ones. Claude uses its
  authenticated native inbox, with credentials kept in Windows DevSecrets.
- Managed native Codex launches, qualified receiver addresses, correlated replies,
  bounded delivery records, and explicit unavailable/uncertain outcomes. Sending
  never silently resumes a saved copy of an already-running session.
- `sesres`, an executable shorthand for `sessions --restart`, usable as `! sesres`.

See `docs/SESSION_MESSAGING.md` for setup, timing, permissions, and supported receivers.

## 3.10.0 — 2026-11-04

### Changed

- `state.json` no longer carries the bookmarks' record fingerprints. A bookmark on a long
  transcript lists one fingerprint per record, and these made up 17 of the file's 18 MB,
  rewritten whole on every rename, hide or copy. They now live in `checkpoint-records`
  beside it, one file per list named by its own hash, and are read only when a transcript
  has been rewritten and a bookmark has to be found again. On this machine the state file
  went from 18.3 MB to 0.17 MB; loading it takes 2 ms instead of 29, saving 8 ms instead
  of 48. The first `sessions` start after upgrading moves them, unless another process
  wrote the file since it was read, in which case a later start does. Lists that no
  bookmark names and nothing has used for a week are deleted at startup.
- An older `sessions` still running reads the new bookmarks safely: it can still tell
  whether a copy changed, but cannot find a bookmark again in a transcript that was
  rewritten since, and reports that copy as one it cannot verify until it is restarted.
- Copying a conversation into another harness writes the state file once instead of twice.

## 3.9.1 — 2026-11-04

### Changed

- `sessions -q "..."` brings the rows it found up to date first. As soon as the table is
  drawn, the matching sessions and their copies in other harnesses are reread from their
  own files: titles, times, activity counts, whether they are open, and which copy of a
  conversation is current. That takes about a second; the rest of the list is reread in the
  background as before. Names given in `sessions` are kept. A search matching more than
  300 sessions is left to the full refresh. The status line says "updating matches" while
  it runs.
- Claude Code and Codex can reread a single session. The reread lists it exactly as a full
  scan would, and was checked against one for every session on a machine with 6,300.

## 3.9.0 — 2026-11-04

### Changed

- Enter opens a session straight away instead of waiting several seconds for the session
  refresh. Before launching it checks only what that row depends on: which copy of its
  conversation is current, read from the copies' own transcripts, whether any of them is
  open, and for a supervisor, the session it watches. A copy made since the saved list was
  written still waits for the refresh. The refresh finishes behind the launch and saves
  the list for next time; sessions that lost their terminal are tidied up on the next start.
- `--restart` and `--reopen` find the session this tab last ran in the saved list instead
  of scanning every session first, then make the same check before reopening it. A session
  started since the list was saved is still found by a scan.
- The supervisor hook, which runs on every prompt of a hooked Claude Code or Codex
  session, no longer loads the harness adapters or psutil: about half a second less per
  prompt. The harnesses are now installed the first time anything asks for one.

## 3.8.6 — 2026-11-04

### Fixed

- Keys answer at once in every view. Moving the cursor in the All or Agent view took about
  half a second and each typed search character over a second, because the whole table was
  rebuilt from every session several times per key. Rows are now rebuilt only when something
  that shapes them changes, each session's search text is kept until the session changes,
  and a search that grows filters the previous matches instead of every session.
- The background refresh finishes about a third sooner. The table no longer redraws ten
  times a second while it waits, which slowed the refresh several times over in larger
  views; files are checked from the folder listing instead of one at a time; only the
  command lines of programs that can run a harness are read when looking for open
  sessions; the result is handed to the table before the saved list is written; and the
  saved list, state and caches are read and written with orjson when it is installed.
- A Codex rollout that is missing from Codex's own database is no longer reopened on every
  refresh, and the threads query reads only the columns `sessions` uses.
- The table appears a little sooner: psutil is loaded only when it is needed.
- A cache or state save that cannot finish no longer leaves its temporary file behind, and
  leftovers from processes that have exited are cleared, along with caches from older
  versions. Cached details of files that no longer exist are dropped.
- The warning "could not verify the start time of a live Codex process" no longer appears
  for a shell whose command line merely mentions Codex.
- The background refresh and the table can no longer change the saved state at the same
  moment.

### Added

- `AI_SESSIONS_CACHE_DIR` moves the cache folder, like `AI_SESSIONS_STATE_FILE` does for
  the state.

## 3.8.5 — 2026-11-04

### Fixed

- The session table appears about three times sooner: around 1 second instead of 3 on a
  machine with 6,000 sessions, with or without `-q`. The first draw no longer rereads every
  copied conversation's transcripts to work out which copy is current; it uses the answer
  from the last refresh, which checks again behind it. The saved list stores each title
  once, and is written to a new file, so an older `sessions` still running is unaffected.
- `sessions` is ready to open a session in about half the time. Enter waits for the session
  refresh; that refresh no longer rereads transcripts that have not changed, walks each
  harness's folders once, checks files in parallel, and keeps titles and previews to 1,000
  characters instead of whole pasted prompts, which had grown the saved list to 52 MB.

## 3.8.3 — 2026-11-03

### Added

- `! sessions --restart --claude`, `--codex` or `--opencode` restarts a session in another
  harness: the conversation is copied across and reopens there in the same tab. Without a
  harness flag, `--restart` still reopens it where it was. `--launch-tool NAME` works too,
  and `--reopen` now honours it.

### Fixed

- Choosing another harness for a supervisor (`x`, then Enter) opens it there again. Since
  3.8.2 it reopened in the harness it was made in. The supervisor is copied across and
  keeps its folder, hooks and name; its old copy stays out of the main list.
- A conversation copied into Claude Code opens on your default model. Copies were credited
  to Opus 5, and Claude Code resumes a session on the model of its last reply, so every
  copy opened on Opus 5 until you changed it.
- A timed-out OpenCode import no longer leaves its temporary copy of the conversation behind
  on a busy Windows machine.

## 3.8.2 — 2026-11-03

### Added

- `! sessions --restart`, typed inside a Claude Code or Codex session that `sessions` opened,
  closes the session and reopens it in the same tab, so it picks up new settings, names or a
  new login in one step. A launch mode given on the original command line is kept, and a
  Codex app-server other windows depend on is left running.
- A HISTORY column shows the harnesses each conversation has passed through, such as
  `C→A→C` (C for Codex, A for Anthropic's Claude Code, O for OpenCode). Press `i` for the
  same history with each harness's full name and the date the conversation moved to it.

### Fixed

- The list shows each session's name as its harness shows it, so a rename made inside Claude
  Code or Codex is never overridden. A supervisor's name is published to its harness once and
  then left alone, and a name given in `sessions` gives way once the harness is renamed.
  Only a name the harness could not record is kept in `sessions` alone.
- Enter on a supervisor row, and `--resume` of a supervisor, open it as a supervisor, with its
  folder and its name; they had opened it as an ordinary session.

### Changed

- Copies made in another harness keep the original name, without `(from Codex)` or
  `(from Claude)` added. Opening a session drops any such suffixes it already has; the
  harnesses they named are kept in its history first.

## 3.8.1 — 2026-11-02

### Added

- `sessions --reopen` resumes the session a Windows Terminal tab last ran, after you quit it,
  so the harness picks up new settings, names or a new login without searching for the
  session. It follows the conversation to its current copy and reopens a supervisor as a
  supervisor; in a fresh tab it lists the sessions closed most recently. After a session
  closes, `sessions` prints this reminder.

### Fixed

- `sessions --reattach` reopens a supervisor through its own launch, so it keeps access to its
  brief files and ledger.

## 3.8.0 — 2026-11-02

### Added

- Supervisor sessions. Press `S` on a session to open a second agent session that watches it
  and answers questions about what it is doing, or `sessions --supervise ID` from the command
  line. The first `S` asks which harness to use; later presses resume the same supervisor.
  On a supervisor, `S` jumps to its target and never offers to stop an open one. A session
  with a supervisor shows `◉`, and its supervisor is listed directly under it as
  `◎ Supervisor: <title>` with the `Super` origin. The details pane names each row
  `MAIN SESSION` or `SUPERVISOR`, `i` shows "supervised by" and
  "supervises", and `R` in `i` resets a supervisor so the next `S` starts a fresh one.
- `sessions --target-log ID` prints what a supervisor sees of its target, and
  `sessions --install-hooks` / `--uninstall-hooks` add and remove the Claude Code and Codex
  hooks that feed it. Codex asks to re-trust its hooks after an install.
- Compressed sessions are listed. Codex rollouts stored as `.jsonl.zst` no longer disappear, nor do
  rollouts missing from Codex's database. `z` or `--compression` chooses uncompressed (the
  default), compressed or both, and the header counts matches the filter is excluding.
  The first refresh indexes existing compressed sessions once, in the background.
- Sessions idle for 30 days are compressed automatically, hidden ones included. `Z`, `--compress`,
  `--decompress` and `--compress-idle` do it by hand, `[retention] compress_after_days` sets the
  days, and opening a compressed session decompresses it after asking. Only uncompressed sessions
  can be bridged or get a new supervisor.

### Fixed

- A supervisor's name now reaches its harness, so its terminal tab reads `Supervisor: <title>`
  instead of a title the harness generated from the first question. A new Claude Code
  supervisor starts with `--name`; a new Codex supervisor is stored already named and resumed,
  because Codex cannot name a session it starts. Existing supervisors are named the next time
  they are opened.
- Renaming a Codex session now reaches Codex. Codex reads a paginated thread's name only from
  its database, so renames appended to `session_index.jsonl` were ignored for every session on
  the current format; renames now go through Codex's own `thread/name/set`, and the list shows
  the name Codex shows.
- A rename applies to every copy of a conversation, so whichever harness opens it next shows
  the same name. Claude Code renames now also set the name shown in its prompt box and tab.
- Bridging no longer stacks a `(from …)` suffix on every round trip. A copy names only the
  harness it came from, and a copy that older versions named
  `elevate-futures (from Codex) (from Claude) (from Codex)` is tidied when it is next opened.

## 3.7.5 — 2026-10-30

### Fixed

- Start in about 0.7 s instead of 6 s or more. Checking a linked conversation's bookmarks
  reread every copy from its first record on each launch, a cost that grew with every
  message. Where each bookmark was found is now remembered across launches and trusted
  while the file has only been appended to; a check that failed is not repeated until
  the file changes.
- Bookmarks taken before semantic fingerprints no longer break when Codex 0.165 merges a
  resume's AGENTS.md instructions and environment context into one message and rewrites
  the compactions that embed it. Every message and tool record must still match exactly
  and in order.
- Preserve conversation-record fingerprints alongside new linked-session bookmarks so
  formatting and instruction-metadata rewrites do not strand valid histories. Actual
  conversation edits or missing records still block automatic routing.
- Separate standard Codex instruction envelopes and compaction bookkeeping from conversation
  fingerprints, retaining checks on real messages, tool traffic and summary content.
- Count legacy Codex response-only prompts and paginated user events, deduplicating
  paired representations. Refresh the discovery cache once for corrected metrics.

### Added

- Persistent in-app recovery for an exact native copy in a blocked group. Enter asks
  before making it the continuing history, backs up utility state, and preserves other
  copies separately; `x` remains available to choose the destination harness.

### Changed

- Find a Codex rollout from the creation date in its id instead of walking every session
  folder for each lookup, trimming the background refresh by about 1.5 s.
- Show Updated ages in days, hours, and minutes.
- Explain validation blocks on native rows instead of claiming the session is missing
  or telling the user to expand a row that cannot expand.

## 3.7.4 — 2026-10-24

### Fixed

- Launch-harness choices, renames, and visibility changes made during a background refresh
  now survive its completion. Selecting Claude no longer silently falls back to Codex when
  Enter applies rows loaded before the selection changed.

### Changed

- Session lists show `standalone` or `linked` in a separate CONNECTION column. STATUS shows
  `latest`, `superseded`, `diverged`, or validation problems independently; standalone sessions
  have no copy status and no warning color.

## 3.7.3 — 2026-10-13

### Fixed

- A session refresh on Windows took about 50 seconds because the Claude discovery cache never
  matched: it recorded each transcript's ctime from the open file, which on Windows is the last
  metadata change, but compared it with the path's ctime, which is creation time. Every
  transcript was reread in full on every refresh; a warm refresh now takes about 5 seconds, and
  pressing Enter no longer waits on the slow refresh.
- Startup re-parsed every record after each conversation's checkpoint to decide whether it had
  changed (about 60,000 records here). Validated progress is now remembered on disk, so only
  records appended since the last look are parsed; malformed records and in-place rewrites are
  still reported as unstable. Loading the session list at startup dropped from about 2.5 to 0.3
  seconds.
- Collapsing whitespace in titles and prompts no longer uses a regular expression.

## 3.7.2 — 2026-10-07

### Added

- `sessions --reattach` (Windows) resumes the session the current Windows Terminal tab lost,
  stopping a copy still running detached in that tab first; in a fresh tab it lists every session
  that lost its terminal.

### Fixed

- Opening an open session on Windows no longer stops at "Already open in Windows Terminal …;
  exact tab focusing is not supported." It offers to stop the running copy and resume it in the
  current terminal, and does so without asking when that copy has lost its terminal and is idle.
- Codex's shared app-server daemon is no longer mistaken for a terminal holding the last thread
  it served, which marked unrelated Codex sessions as open.
- Killing `sessions` no longer leaves Claude Code running in the tab, sharing keystrokes with the
  shell: the harness runs in a kill-on-close job tied to the launcher. Codex is excluded because a
  Codex window can start the shared daemon other windows depend on.
- Harnesses already left detached that way are stopped when `sessions` starts, once they are
  between turns, and starting a session in a tab stops one detached there.

## 3.7.1 — 2026-09-28

### Fixed

- Upgrading a Codex session no longer breaks the conversation that tracks it. Migration rewrites
  the rollout in place, which left every tracked byte position pointing into different content,
  so the conversation read as unstable and refused to launch in any harness ("No native session
  is available for this row"). Each tracked position is now anchored to its record before the
  upgrade and relocated after the same record afterwards, keeping its classification. A session
  whose tracking is not settled, or cannot be anchored, is left on the legacy format instead.
- A subagent row resumed through its parent no longer passes the subagent's own file to the
  parent's storage upgrade.

## 3.7.0 — 2026-09-24

### Added

- Harnesses can declare an `upgrade_storage` capability, letting a provider modernize its own
  stored session before a native resume.
- Codex sessions recorded before paginated thread history are migrated on resume, so rewinding
  and prompt editing work instead of failing with `thread/revert only supports paginated
  threads`. A session open in another Codex process is reported and left untouched.

## 3.6.0 — 2026-09-24

### Added

- Every resume records the harness, the selected session, the id actually requested, and the
  command that ran. `sessions --launch-log` prints that history, so a harness that opens a
  different conversation than the row that was picked can be told apart from a wrong id.

## 3.5.1 — 2026-08-28

### Fixed

- Interactive browser columns now have exact widths and guaranteed gutters. Large activity
  counts and full-width labels are ellipsized within their own cells instead of shifting every
  column that follows them.

## 3.5.0 — 2026-08-27

### Added

- A conversation-centered browser collapses cross-harness materializations into one logical row
  and groups same-title project threads into an expandable container without inferring lineage.
- Press `i` for complete session, lineage, related-thread, storage, and native-ID details. Press
  `f` to focus the selected project and again to return to all projects.
- Activity is reported consistently as `xt yc zp`: semantic conversation turns, native
  compactions, and direct user prompts. Claude, Codex, and OpenCode collect these metrics with
  incremental, rewrite-safe caches.

### Changed

- Normal rows use human date/time collision labels and no longer expose native ID/hash fragments.
- Conversation labels now say `lineage head`, `superseded copy`, `diverged branch`, `independent
  thread`, or `untracked` instead of the ambiguous `current` shorthand.
- Plain `--list` output uses the compact activity triple while JSON retains full native identity
  and compatibility fields.

### Fixed

- Equivalent non-diverged cross-harness heads remain launchable, while divergent heads remain
  blocked by authoritative lineage resolution.
- Filtering, refresh, and child disappearance cannot transfer selection or launch intent to a
  sibling session, and hidden/filtered older copies cannot replace an authoritative head.
- Same-inode transcript rewrites invalidate aggregate activity caches; partial records remain
  safely replayable, and pre-3.5 session catalogs migrate their missing activity fields.

## 3.4.3 — 2026-08-27

### Changed

- Duplicate titles are disambiguated only when needed with stable, minimally unique session-ID
  suffixes in the interactive browser and plain list output.

### Fixed

- Tracked conversations now accept launch targets only from their recorded conversation members;
  transcript-derived IDs cannot redirect preparation or the final resume command to an unrelated
  existing session.
- Arbitrary transcript ID mentions no longer cross-link two human or subagent sessions. Legacy
  discovery remains available when at least one side is a writer-created bridge artifact.
- Ambiguous name errors now point to the browser detail and JSON output for the exact session ID.

## 3.4.2 — 2026-08-27

### Fixed

- Claude Code and Codex bridges now use the configured 1M-context operating policy, reserving 5%
  for target scaffolding instead of applying the obsolete 200k/256k compatibility floors. The
  complete latest native compaction window passes through whenever it fits that policy.

## 3.4.1 — 2026-08-27

### Fixed

- Bridges now refuse before the target writer when selection would drop messages or truncate an
  anchor; explicit `allow_lossy = true` preserves the legacy behavior while reporting exact losses.
- Moved legacy sources preserve conservative `advanced` status by removing the current checkpoint
  that shadowed `cursor = 0`, preventing stale ancestor reuse or misclassification.

## 3.4.0 — 2026-08-26

### Added

- Recover a live tmux session whose terminal died. When tmux reports no client on the session,
  `sessions` opens one attached to the exact window and pane instead of reporting a dead end.
  The terminal is resolved from `[terminal] command`, then `$TERMINAL`, then a probe list;
  kitty is a fallback default rather than a dependency.

### Fixed

- Only trust a `tmux:<session>` window title as an exact match on the title field, and only
  while a client really is on that session. Substring matching against the whole `wmctrl` line
  let session `2` focus a window titled `tmux:21`, and the title is fixed at launch, so a client
  that had since switched sessions could silently focus the wrong one.
- Every focus failure now names its recovery, including the literal `tmux attach-session`
  command. A session that is attached but unreachable explains why no second terminal was
  opened, since two clients on one session leave both sized to the smaller window.

## 3.3.0 — 2026-08-25

### Added

- A discoverable `t` tool picker can show any non-empty combination of registered harnesses while
  keeping `Tab` as a fast single-tool preset cycle.
- The interactive browser paints a persisted session catalog immediately and refreshes provider
  discovery and liveness in the background.

### Changed

- Process discovery collects expensive start-time identity only for possible harness processes.
- Codex, Claude Code, and OpenCode discovery extract cross-harness evidence from semantic messages
  instead of regex-scanning every serialized log byte; OpenCode discovery is persisted by exact
  database snapshot identity.
- Native references discovered during a provider scan are reused during reconciliation instead of
  repeatedly searching provider storage.

## 3.2.1 — 2026-08-25

### Fixed

- Resolve duplicate Claude liveness claims by refresh time instead of filename order. A Claude
  process that moves to another session without rewriting its registry entry keeps advertising
  the old one for as long as it runs, so two live processes can claim one session id. The claim
  still being refreshed now wins, and the collision is reported with the losing PID, because a
  stale claim is why focusing a session could open an unrelated terminal.

## 3.2.0 — 2026-08-23

### Added

- OpenCode as a first-class third harness: authoritative SQLite discovery, semantic full/latest
  reading, staged revert and completed-compaction handling, official-import writing, dynamic model
  budgets, exact resume, title publication, liveness, search/list/detail UI, and configuration.
- Storage-neutral `NativeRef` identities, opaque adapter-owned checkpoints, tri-state exact
  availability, and dynamic target preparation for shared mutable native stores.
- Repeatable isolated real-OpenCode Linux and Windows release verifiers.

### Changed

- All six Claude/Codex/OpenCode bridge directions and three same-harness identities now share one
  adapter-neutral conversion and conversation-head routing path.
- Conversation state records opaque checkpoints while preserving legacy integer cursors under
  schema 6 for rollback compatibility.
- Unknown OpenCode semantic part kinds fail conservatively with a user-visible warning rather than
  being silently ignored.

### Fixed

- Child-session details distinguish harnesses that resume a parent from those, including OpenCode,
  that resume the exact child ID.
- Missing or unavailable shared-store rows cannot be mistaken for available sessions merely because
  the database file still exists.
- Concurrent OpenCode writes, unrelated database activity, title-only changes, failed compaction
  attempts, import ID reminting, and timeout-after-commit recovery are classified without losing or
  silently promoting conversation work.
