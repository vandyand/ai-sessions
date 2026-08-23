# Harness contract

The end state is one core that understands conversations and one adapter per harness that
understands native storage and process behavior. No adapter may know about another adapter,
and no bridge path may be implemented pairwise.

## Domain boundaries

A **native session** is an append-only transcript that one harness can resume by its own
id. A **conversation** is the utility-owned identity that survives movement between
harnesses. A **member** is one native materialization of that conversation.

Each member records:

| Field | Meaning |
| --- | --- |
| `tool`, `session_id`, `storage` | Native identity and transcript location |
| `generation` | Causal materialization step; never inferred from wall-clock time |
| `frontier` | Equivalence token shared by members containing the same carried history |
| `cursor` | Safe byte boundary consumed from this append-only transcript |
| `cwd`, `title`, `updated` | Discovery/display metadata, not routing authority |

If no member has meaningful records after its cursor, every member at the maximum
generation is an equivalent head. If exactly one maximum-generation member advances, it is
the head. Two advanced members, or an older member advancing after a newer generation
exists, is divergence. Automatic launch must stop on divergence.

The selected row is only a user entry point. Launch resolves the conversation first,
follows its head, then either reuses an equivalent member in the requested harness or
materializes a new one. A native UUID mentioned in transcript prose is never identity.

## Current bridge adapter

`bridge.Harness` currently owns the operations needed by conversion and head detection:

```text
name, label
read(path, options) -> Transcript
write(cwd, turns, title) -> (native_id, path)
locate(native_id) -> bool
change_status(path, byte_cursor) -> unchanged | changed | unstable
budget -> BudgetPolicy(context_tokens, usable_fraction, chars_per_token, source)
```

`read` projects native records into the shared `Turn` model. `write` must produce a truly
resumable native transcript, including any separate records needed for both model context
and visible scrollback. `change_status` recognizes conversation/tool activity and ignores
metadata-only appends such as renames. Missing, replaced, truncated, or partially written
append-only storage is `unstable` so the core fails conservatively. If the newest
generation is unavailable and no equivalent native member survives, launch refuses to
promote an older generation.

The cursor returned with a bridge is captured before the read begins and aligned after the
last complete JSONL record. Provisional state uses the same alignment, so completing a
partial record cannot strand later retries inside that record. This is intentionally a
lower bound: concurrent work may be copied twice on a later hop, but it cannot be marked
as consumed without having been observed.

Validated status is cached only for an unchanged native file snapshot. `unstable` results
are never cached, because a sharing violation or other temporary read failure must remain
retryable without requiring the provider to modify the transcript first.

The budget policy is target-owned because context assumptions are adapter capabilities, not
core routing rules. It is a conservative unknown-model default, not a claim that the bridge
can inspect the model, system prompt, tools, or compaction threshold that will exist at
resume time. Core resolves optional `max_tokens` / legacy `max_chars` configuration into one
immutable applied `Budget`, then uses that same object for selection and the handoff note.
Adding a harness must not add target-name conditionals to budget resolution.

## Complete adapter target

The remaining provider branches in `app.py` should move behind the same adapter in this
order:

1. `discover() -> Iterable[NativeSession]` — enumerate sessions and provider metadata.
2. `resume_argv(native_id, mode, options) -> list[str]` — construct a shell-free command.
3. `publish_name(session, title) -> PublishResult` — append the provider-supported name.
4. `inspect_liveness(sessions) -> Mapping[id, NativeProcess]` — identify open sessions.
5. `focus(process) -> FocusResult` — optional platform capability, never required to resume.
6. The existing `read`, `write`, `locate`, and `change_status` conversion operations.

Capabilities should be explicit (`rename`, `focus`, `subagents`, `compaction`, budget policy,
and so on)
rather than detected with `if tool == ...` in core code. Provider-specific caches and
database schemas belong inside the adapter. The core owns filtering, display, conversation
state, head resolution, launch policy, and error presentation.

Adding a third harness is complete when its adapter passes shared contract fixtures for:

- discovery and stable native identity;
- native resume command construction in every launch mode;
- transcript projection and resumable materialization;
- semantic tail detection that ignores metadata;
- round trips through each existing harness without pairwise code;
- rename/liveness capabilities when declared; and
- concurrent append safety and explicit divergence.

Shared budget fixtures additionally require `context_tokens > 0`,
`0 < usable_fraction <= 1`, `chars_per_token > 0`, and non-empty provenance,
an unset configuration to resolve through that policy, and the same explicit legacy
`max_chars` value to remain identical across all targets. Selection accepts a cost callback so
the full conversation-log phase can account for native-versus-projected forms and assembly
overhead without rewriting the selection algorithm. That callback prices a complete candidate
sequence in the applied budget's character unit; a per-message callback is insufficient because
same-role merge separators depend on adjacency. The selection metric also supplies a matching
`truncate(turn, limit)` operation; core verifies that sequence cost is monotone and that a
truncated anchor fits its assigned share. P3's default metric prices the text produced by
`merge_runs`, including separators and stripping, in `Budget.chars`.

## Persistence and recovery

State schema 6 stores conversations and member cursors in `state.json`; this is the routing
authority. New materializations also carry an `[ai-sessions-provenance v1]` JSON marker with
the conversation id and immediate source. The marker is currently for auditability and a
future recovery tool. Losing `state.json` still loses group identity; scanning transcripts
to rebuild it belongs to the full conversation-log phase.

Version 5 bridge records have no safe cursor. Migration marks a surviving target copy as
possibly advanced so the utility may duplicate history but will not silently choose its
ancestor. Provider transcripts remain append-only and are never rewritten in place.

## Deliberate non-goals of the head-routing phase

- Per-message provenance and verbatim same-origin replay.
- Automatic merge or winner selection for divergent heads.
- Reconstructing conversation state solely from provenance markers.
- Editing an existing native materialization to avoid creating the next one.
- Treating timestamps, titles, UUID-shaped prose, or process state as causal evidence.
