# Harness contract

The end state is one core that understands conversations and one adapter per harness that
understands native storage and process behavior. No adapter may know about another adapter,
and no bridge path may be implemented pairwise.

## Domain boundaries

A **native session** is the exact `(session_id, storage)` reference that one harness can resume.
Storage may be a per-session append-only file or a shared mutable store containing many identities.
A **conversation** is the utility-owned identity that survives movement between harnesses. A
**member** is one native materialization of that conversation.

Each member records:

| Field | Meaning |
| --- | --- |
| `tool`, `session_id`, `storage` | Native identity and transcript location |
| `generation` | Causal materialization step; never inferred from wall-clock time |
| `frontier` | Equivalence token shared by members containing the same carried history |
| `checkpoint` | Opaque JSON-safe semantic state captured by the owning adapter |
| `cursor` | Legacy integer JSONL checkpoint retained for schema-6 rollback |
| `cwd`, `title`, `updated` | Discovery/display metadata, not routing authority |

If no member has meaningful records after its checkpoint, every member at the maximum
generation is an equivalent head. If exactly one maximum-generation member advances, it is
the head. Two advanced members, or an older member advancing after a newer generation
exists, is divergence. Automatic launch must stop on divergence.

The selected row is only a user entry point. Launch resolves the conversation first,
follows its head, then either reuses an equivalent member in the requested harness or
materializes a new one. A native UUID mentioned in transcript prose is never identity.

## Runtime adapter

One immutable `HarnessAdapter` owns the complete native boundary:

```text
name, label, short_label, order, home
default_command, dangerous_args, resume_args(...)
source_kinds, id_patterns, scratch_patterns
discover(context) -> Iterable[NativeSession]
publish_name(session, title) -> note
inspect_liveness(context, home, sessions) -> Mapping[id, pid]
read(ref, latest_window) -> ReadSnapshot(transcript, checkpoint)
write(cwd, turns, title, prepared) -> NativeWrite(ref, checkpoint | None, notices)
resolve(native_id) -> NativeRef | None
availability(ref) -> available | unavailable | unknown
checkpoint(ref) -> int | str
change_status(ref, checkpoint) -> unchanged | changed | unstable | unknown | unsupported
prepare_target(command, cwd, options) -> PreparedTarget
budget -> BudgetPolicy(context_tokens, usable_fraction, chars_per_token, source)
```

The ordered registry is dynamic and process-local. Built-ins push complete adapters during
package initialization; the registry never imports providers. Core consumers query it at call
time, so scoped late registration participates in CLI choices, configuration, discovery,
rendering, liveness, naming, and conversion without refreshing an import-time snapshot.

`read` atomically projects native records and captures their adapter-owned checkpoint. `write` must
produce a truly
resumable native transcript, including any separate records needed for both model context
and visible scrollback. `change_status` recognizes conversation/tool activity and ignores
metadata-only changes such as renames. Missing, replaced, truncated, malformed, locked, or
partially written native storage is unavailable, unknown, or unstable as appropriate, so the core
fails conservatively. If the newest
generation is unavailable and no equivalent native member survives, launch refuses to
promote an older generation.

JSONL adapters use a complete-record byte boundary. Shared-store adapters instead own a semantic
digest over every native field that can alter either latest- or full-window projection. Core treats
both as opaque: it never orders, increments, subtracts, or tests checkpoint truthiness. A bridge
reads content/checkpoint from one native snapshot, then asks the source adapter to prove it remained
unchanged before writing the target.

Validated status may be cached only against an adapter-correct native snapshot token. `unstable`,
`unknown`, and unsupported results are never cached, because temporary failures must remain
retryable without requiring the provider to modify native storage first.

The budget policy is target-owned because context assumptions are adapter capabilities, not
core routing rules. It is a conservative unknown-model default, not a claim that the bridge
can inspect the model, system prompt, tools, or compaction threshold that will exist at
resume time. Core resolves optional `max_tokens` / legacy `max_chars` configuration into one
immutable applied `Budget`, then uses that same object for selection and the handoff note.
Adding a harness must not add target-name conditionals to budget resolution.

Selection reserves 4,096 projected characters for bounded handoff metadata. It flattens
tool summaries without merging source messages, prices both message text and same-role
assembly joins through one `SelectionMetric`, selects, and only then merges surviving runs.
A fitting conversation is unchanged. On overflow the first message takes an initial capped
share, the newest takes the remaining anchor allowance, and any additional survivors form
one contiguous newest suffix. The allocation must be monotone: increasing a budget cannot
decrease survivor count or increase dropped-message count. `BridgeResult` and both notices
distinguish selected source-message count, assembled target-message count, dropped messages,
and marker-truncated anchors.

## Runtime ownership

Provider-specific discovery, resume syntax, title publication, liveness evidence, transcript
formats, caches, native-id patterns, and budget policy are all behind the adapter. Generic core
code contains no provider-name routing maps or comparisons outside explicit schema-2
configuration compatibility. Unsupported optional capabilities return an explicit reason;
unknown harnesses never borrow another provider's behavior.

Terminal focus is a platform/core capability over a verified open PID, not a harness hook.
On Windows exact tab focus is unsupported for every current harness; on Linux the same
tmux/desktop-terminal logic applies regardless of which adapter identified the process.

`NativeSession` contains provider discovery data only. Adapters do not construct the utility
`Session` fields that carry local names, visibility, launch selection, conversation identity,
frontiers, superseded state, or divergence. Its source kind is a closed neutral enum
(`interactive`, `non-interactive`, `subagent`, `sdk`), not an unchecked provider string.
It is string-serializable for search/detail/JSON output, while provider facts such as archived
status remain explicit `NativeSession` fields instead of being guessed from source kind.

Capabilities are explicit rather than detected with `if tool == ...` in core code.
Provider-specific caches and
database schemas belong inside the adapter. The core owns filtering, display, conversation
state, head resolution, launch policy, and error presentation.

Adding another harness is complete when its adapter passes shared contract fixtures for:

- discovery and stable native identity;
- native resume command construction in every launch mode;
- transcript projection and resumable materialization;
- semantic tail detection that ignores metadata;
- round trips through each existing harness without pairwise code;
- rename/liveness capabilities when declared; and
- concurrent append safety and explicit divergence.

Shared budget fixtures additionally require `context_tokens > 0`,
`0 < usable_fraction <= 1`, `chars_per_token > 0`, non-empty provenance, and a derived default
at or above `MIN_BRIDGE_CHARS`. Unset config resolves through target policy. An explicit legacy
`max_chars` other than schema-1 `950000` remains identical across targets; that exact schema-1
machine default migrates to each target's policy. A bare per-message cost is insufficient because
same-role merge separators depend on adjacency, so `SelectionMetric` supplies
`item_cost(turn)`, `join_cost(left, right)`, and `truncate(turn, limit)` in `Budget.chars`.
Core validates non-negative costs and truncated anchors against their shares; the selector
scans once with a running total rather than repricing whole candidates. P3's default uses
`len(text)` plus a conservative two-character same-role join, which upper-bounds the text
produced by `merge_runs`; P4 can replace the complete metric.

## Persistence and recovery

State schema 6 stores conversations and member checkpoints in `state.json`; this is the routing
authority. New materializations also carry an `[ai-sessions-provenance v1]` JSON marker with
the conversation id and immediate source. The marker is currently for auditability and a
future recovery tool. Losing `state.json` still loses group identity; scanning transcripts
to rebuild it belongs to the full conversation-log phase.

Version 5 bridge records have no safe checkpoint. Migration marks a surviving target copy as
possibly advanced so the utility may duplicate history but will not silently choose its
ancestor. Provider conversation content is never rewritten in place; metadata-only title
publication follows the exact bounded operation declared by its adapter.

State and configuration are forward-compatible data. A build whose registry does not know a
harness may hide or refuse to operate on that harness, but load/save must preserve its bridge
records, conversation members, launch preference, and keyed provider profile verbatim. An
older or partial registry must never erase routing authority written by a newer adapter.
Such a member has an `unknown` availability status: it blocks automatic promotion past its
possibly newer frontier but does not invoke a missing adapter or crash known-harness listing.

Cross-harness discovery evidence is format-agnostic and registry-sensitive. Transcript scanners
match the union of registered native-ID prefilters and cache a digest of that pattern set; adding
an adapter forces historical bytes to be rescanned. Candidate tokens are deduped and capped at
4,096 per transcript in first-scan order with a persisted truncation flag. This item-count cap is
unrelated to the 4,096-character handoff-note reserve and uses a separate constant. Truncated evidence can establish positive
discovered/existing-ID matches but cannot support a negative "no counterpart" conclusion.
Claims are not exclusive—ID shapes overlap—so evidence resolves first against IDs returned by
discovery and then against verified native existence. No adapter names or imports another.
If multiple adapters verify the same otherwise-undiscovered token, resolution remains ambiguous
and produces no cross-origin claim.

## Deliberate non-goals of the head-routing phase

- Per-message provenance and verbatim same-origin replay.
- Automatic merge or winner selection for divergent heads.
- Reconstructing conversation state solely from provenance markers.
- Editing an existing native materialization to avoid creating the next one.
- Treating timestamps, titles, UUID-shaped prose, or process state as causal evidence.
