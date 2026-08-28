# Conversation-centered session browser — research

## Problem evidence

The current browser renders one `Session` per provider-native record. It therefore shows every
cross-harness materialization and every legacy independent thread as peers, while collision labels
expose short native IDs. `conversation_status()` calls a non-superseded tracked member merely
`current`, which is true only within one utility lineage and reads as a global recommendation.

The `MSGS` column is populated from adapter discovery metadata. On real compacted Nautilus
sessions, a row displaying three messages corresponds to more than 1,500 transcript turns and
five compactions. The value is neither a useful size signal nor a reliable statement of retained
context.

## Architecture direction

- Keep `Session` as the native materialization model and add neutral activity metrics to it.
- Build immutable browser view rows after native filtering. View rows own grouping, expansion,
  human labels, representative selection, and stable identity; native sessions retain all launch,
  rename, hide, and focus semantics.
- Derive tracked groups only from `conversation_id`. Derive presentation-only independent groups
  from normalized project identity plus normalized display title.
- Compute tracked groups and authoritative heads from the full discovered/state snapshot before
  applying tool/origin/visibility/query/project filters. Filters control visibility, not causal
  routing; an eligible older child cannot become a substitute head.
- Use adapter discovery/read semantics for activity counting and cache by native checkpoint or
  storage fingerprint. Never parse the same large unchanged transcript on every redraw.
- Keep machine-oriented JSON native-session based for backward compatibility; add metrics rather
  than replacing existing keys abruptly.

## Product decisions

- A collapsed tracked row is actionable because head routing is authoritative.
- A collapsed independent-thread group is not actionable; Enter expands it, preventing a hidden
  “newest wins” guess.
- Expansion is transient browser state and is not persisted to utility state.
- Project focus uses normalized project identity rather than a mere directory basename. Different
  repositories with the same leaf name remain separate.
- Human collision labels prefer local start date, then time, then ordinal. IDs never leak back
  into the row as a fallback.
- A view row owns a stable row ID and `Session | None` launch target. Only rows with a concrete
  target may launch, focus, rename, hide, or change launch harness. Synthetic independent-group
  rows expand; they never guess a child.

## Activity contract and cache lifecycle

- `turn_count` counts semantic native user and assistant conversation messages, excluding tool-only,
  system/developer, known provenance, and duplicated Codex replacement-history carry records.
- `prompt_count` counts direct semantic user prompts. `compaction_count` counts all observed native
  compaction events, readable or opaque; a semantic assistant summary can also contribute one turn.
- A partial final record contributes nothing until complete. Append caches resume only from the
  last complete boundary. Inode/file identity change, shrink, invalid offset, semantic-classifier
  version change, or prior incomplete tail forces a safe rebuild. Shared-store caches use the
  adapter's database/WAL semantic fingerprint.
- Provider activity caches persist aggregate counters and offsets, not turns. Unchanged warm scans
  perform bounded metadata/checkpoint validation and no transcript re-projection; cold scans remain
  streaming with peak memory independent of transcript length.

## Compatibility risks

- Existing tests and scripts may construct `Session`/`NativeSession` with only `message_count`.
  New fields therefore need defaults and `message_count` should remain as a compatibility alias or
  serialized field during this release.
- Full transcript counting can regress startup time and memory. Streaming readers/caches and
  checkpoint invalidation are required; tests need asymptotic and warm-cache assertions.
- Group filtering order matters. Native visibility/tool/origin/query filters should establish
  eligible children before view grouping so hidden or filtered children do not unexpectedly
  launch through a visible aggregate.
- Rename/hide operations require an exact native child. Synthetic groups must expand rather than
  silently applying mutations to every child.
