---
title: "Conversation-Centered Session Browser"
status: completed
date: 2026-08-27
priority: 20
---
# Conversation-centered session browser

Status: complete on `feature/session-browser-ux` (`7edfb45`, implementation rooted at `d2e5357`
and hardened through `ff4e91e`).

Replace the provider-file-oriented browser with a conversation-oriented view that is easy to
scan without weakening the native-session and lineage guarantees used for launch routing.

## User outcome

The default list shows one row per known logical conversation. Independent sessions with the
same project and title appear as one expandable thread group. Superseded cross-harness copies
are available in lineage details and expansion, but no longer compete as peer rows with the
current head. A project can be focused directly from the selected row.

## Required behavior

1. Collapse all members of a tracked utility conversation to one default row. Select the
   authoritative current head for display and launch; never infer ancestry from title, path,
   time, or native IDs.
2. Group two or more independent sessions only when their normalized display title and project
   identity match. The group remains explicitly independent, is expandable, and does not grant
   shared launch or bridge ancestry.
3. `i` opens a details view showing project, logical thread, status, harness, exact timestamps,
   compact activity, latest prompt, ordered lineage/related threads, and the full native ID.
4. Replace `[current]`, `[superseded]`, and `[diverged]` with the precise vocabulary `lineage
   head`, `superseded copy`, `diverged branch`, `independent thread`, and `untracked`.
5. Render activity as `xt yc zp`: transcript turns, compaction boundaries/summaries, and semantic
   user prompts. Counts must survive compaction and incomplete live tails and must not treat tool
   results or provider scaffolding as prompts.
6. Never display native hash/UUID values in normal rows. Colliding threads use stable human
   labels based on start date/time and an ordinal only when needed. Full IDs remain available in
   details and JSON/list-machine output.
7. Provide a one-action project-focus mode from the selected row, visibly indicate the focus,
   and provide an equally direct way back to all projects.

## Safety invariants

- Rendering and grouping never mutate native transcripts or utility lineage state.
- Enter on a tracked conversation delegates to the existing head resolver; divergence remains a
  refusal unless the existing routing contract gains an explicit user branch choice.
- Enter on an independent thread launches exactly the selected native session.
- A synthetic group row never launches merely because it has a newest child; it expands first.
- Refresh preserves selection by stable view identity where possible and cannot transfer launch
  intent between children.
- Tracked membership and authoritative heads are computed from the complete discovered/state
  snapshot before display filters. A matching historical child may make its conversation visible,
  but can never replace a hidden, filtered, missing, or uncertain head as the launch target.
- Every rendered row carries either one immutable native `Session` target or no target. Tracked
  aggregates target an authoritative head; expanded member rows target that exact member;
  independent-group containers have no target and only expand.
- Active writers, partial JSONL records, missing members, unknown harnesses, and malformed legacy
  state fail conservatively and remain inspectable.

## Deterministic normalization and precedence

- Titles group by normalized whitespace followed by Unicode `casefold()`.
- Project identity is the normalized resolved cwd's nearest existing repository root when a `.git`
  ancestor exists, otherwise the normalized cwd itself. Windows extended prefixes are removed;
  missing paths use lexical normalization without requiring filesystem access.
- Divergence/unknown/unstable/unavailable launch blockers take precedence over presentation labels.
  `lineage head` is used only for a validated single head; `superseded copy` only for a verified
  non-head; `independent thread` for a child of a presentation-only group; `untracked` otherwise.
- If refresh removes an exact selected child, selection falls back to its surviving parent row,
  never to a sibling. Per-native launch-harness preferences remain keyed only by native identity.

## Acceptance evidence

Generated fixtures cover tracked chains, equivalent heads, superseded copies, divergent heads,
same-title independent threads, same-title different projects, exact collisions, hidden and
auxiliary members, active writers, compacted Claude/Codex/OpenCode histories, malformed tails,
refresh/selection stability, narrow terminals, CLI compatibility, and bounded refresh cost.

Release verification covers 541 unit/integration tests, exact real-data grouping of the three
Nautilus threads, 3,416-session cold/warm discovery, a 10,000-row same-title stress case, and a
400-compaction Codex scanner. Independent adversarial review found and closed equivalent-head,
selection-transfer, cache-rewrite, legacy-catalog, ID-leakage, unknown-project, and quadratic
grouping defects before release.
